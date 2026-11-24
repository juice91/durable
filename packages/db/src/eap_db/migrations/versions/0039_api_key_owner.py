"""Personal API keys: an optional owner on `api_keys`.

Revision ID: 0039_api_key_owner
Revises: 0038_capability_snapshots
Create Date: 2026-11-24 12:00:00

`api_keys.owner_subject` is the subject of the person who created the key for themselves (for example
`zitadel:<sub>`), `owner_email` the e-mail they had at that time (a snapshot, for display). Both NULL is a tenant key,
which is what every existing row stays: nothing is backfilled. A key has an owner subject or none, and an e-mail only
with an owner. The owner index serves "my keys" (tenant first, as every query is tenant-scoped).

Expand-only (two nullable columns, one index). Downgrade drops them, which turns personal keys into nothing: the rows
are deleted first, because a personal key without its owner would read as a tenant key with the owner's reach.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039_api_key_owner"
down_revision: Union[str, Sequence[str], None] = "0038_capability_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("owner_subject", sa.Text(), nullable=True))
    op.add_column("api_keys", sa.Column("owner_email", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_api_keys_owner",
        "api_keys",
        "(owner_subject IS NULL AND owner_email IS NULL) OR (owner_subject IS NOT NULL AND char_length(owner_subject) BETWEEN 1 AND 255)",
    )
    op.create_index("ix_api_keys_tenant_owner", "api_keys", ["tenant_id", "owner_subject"])


def downgrade() -> None:
    op.execute("DELETE FROM api_keys WHERE owner_subject IS NOT NULL")
    op.drop_index("ix_api_keys_tenant_owner", table_name="api_keys")
    op.drop_constraint("ck_api_keys_owner", "api_keys", type_="check")
    op.drop_column("api_keys", "owner_email")
    op.drop_column("api_keys", "owner_subject")
