"""Knowledge-base source kinds: connection kind `api_token`, source kind `repository_docs`, ledger status `removed`.

Revision ID: 0030_kb_source_kinds
Revises: 0029_kb_sources
Create Date: 2026-11-18 21:00:00

The GitHub repository documents adapter (second provider) needs a connection kind with one optional token
secret (`api_token`), a source kind for files of a repository (`repository_docs`) and a ledger status for a
file that left the repository (`removed`). Three CHECK constraints are swapped for wider ones: no table
rewrite; the new text is the old one plus a value, so existing rows satisfy it (added NOT VALID, then
validated, as 0015/0022).

Downgrade: `api_token` connections (and the `repository_docs` sources that use them) and ledger rows with
status `removed` are deleted; entities, records and syncs of the deleted sources cascade.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0030_kb_source_kinds"
down_revision: Union[str, Sequence[str], None] = "0029_kb_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.kb_sources (a migration never imports app code).
_SWAPS = (
    ("settings_connections", "ck_settings_connections_kind",
     "kind IN ('oauth2_client_credentials')", "kind IN ('oauth2_client_credentials', 'api_token')"),
    ("kb_sources", "ck_kb_sources_kind",
     "kind IN ('product_catalog')", "kind IN ('product_catalog', 'repository_docs')"),
    ("kb_entity_documents", "ck_kb_entity_documents_status",
     "status IN ('discovered', 'queued', 'failed')", "status IN ('discovered', 'queued', 'failed', 'removed')"),
)


def _swap(table: str, name: str, check: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, check, postgresql_not_valid=True)
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table, name, _old, new in _SWAPS:
        _swap(table, name, new)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM kb_entity_documents WHERE status = 'removed'")
    op.execute("DELETE FROM kb_sources WHERE kind = 'repository_docs'")
    op.execute(
        "DELETE FROM kb_sources WHERE (tenant_id, connection_id) IN"
        " (SELECT tenant_id, id FROM settings_connections WHERE kind = 'api_token')"
    )
    op.execute("DELETE FROM settings_connections WHERE kind = 'api_token'")
    for table, name, old, _new in reversed(_SWAPS):
        _swap(table, name, old)
