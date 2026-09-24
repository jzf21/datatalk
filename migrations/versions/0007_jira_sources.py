"""sources: Jira, as a synced source

Jira is not a SQL engine, and everything downstream of the agent -- captured
datasets, ``materialize()``, dashboard refresh, filter binding, the evals --
assumes one. So a Jira source is *synced* into its own schema of a separate
Postgres database (the sync store) and queried there as a read-only Postgres
source. Nothing downstream learns that Jira exists.

Two changes here:

* ``org_warehouse_connections.type`` admits ``'jira'``, and gains
  ``scope_query`` -- the JQL that scopes what is synced. NULL for the SQL
  engines, and NULL on a Jira source means "everything the account can see".
* ``source_sync_state`` records where each synced source lives in the sync
  store (schema + login role, whose password is encrypted like every other
  secret) and how fresh it is. The schema and role names become identifiers in
  DDL, so their shape is a CHECK: an injection there must be unrepresentable,
  not merely rejected upstream.

Purely additive. Every existing row keeps its type and gets a NULL
``scope_query``.

``tests/test_migrations.py`` asserts this file and the ORM models agree.

Revision ID: 0007
Revises: 0006
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_whconn_type", "org_warehouse_connections", type_="check")
    op.create_check_constraint(
        "ck_whconn_type",
        "org_warehouse_connections",
        "type IN ('clickhouse','postgres','jira')",
    )
    op.add_column(
        "org_warehouse_connections", sa.Column("scope_query", sa.Text(), nullable=True)
    )

    op.create_table(
        "source_sync_state",
        sa.Column(
            "connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("org_warehouse_connections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_name", sa.Text(), nullable=False, unique=True),
        sa.Column("role_name", sa.Text(), nullable=False, unique=True),
        sa.Column("role_password_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("cursor", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_status", sa.Text(), nullable=False, server_default=sa.text("'never'")
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "stats", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.CheckConstraint(
            "last_status IN ('never','running','ok','error')",
            name="ck_syncstate_status",
        ),
        sa.CheckConstraint(
            r"schema_name ~ '^jira_[0-9a-f]{12}$'", name="ck_syncstate_schema"
        ),
        sa.CheckConstraint(
            r"role_name ~ '^dt_src_[0-9a-f]{12}$'", name="ck_syncstate_role"
        ),
    )


def downgrade() -> None:
    op.drop_table("source_sync_state")
    op.drop_column("org_warehouse_connections", "scope_query")
    # A downgrade with Jira rows present cannot satisfy the old CHECK; remove
    # them rather than fail halfway. Their sync-store schemas are left for
    # `datatalk-sync --gc`, which this revision cannot reach.
    op.execute("DELETE FROM org_warehouse_connections WHERE type = 'jira'")
    op.drop_constraint("ck_whconn_type", "org_warehouse_connections", type_="check")
    op.create_check_constraint(
        "ck_whconn_type",
        "org_warehouse_connections",
        "type IN ('clickhouse','postgres')",
    )
