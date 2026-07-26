"""multi-source warehouses: rename the connection table and add engine columns

An org now holds several named data sources, each ClickHouse or Postgres, so
``org_clickhouse_connections`` is renamed and gains ``type``, ``sslmode`` and
``description``.

Purely widening: every existing row is a ClickHouse source and the column
default says so, no backfill needed. Renaming the table (rather than adding a
``type`` column to a misleadingly named one) is cheap here and permanently
confusing later.

``tests/test_migrations.py`` asserts this file and the ORM models agree.

Revision ID: 0002
Revises: 0001
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_TABLE = "org_clickhouse_connections"
_NEW_TABLE = "org_warehouse_connections"


def upgrade() -> None:
    op.rename_table(_OLD_TABLE, _NEW_TABLE)
    # Renaming a table leaves its constraint and index names behind, and
    # "ux_chconn_*" on a table with no ClickHouse in its name is exactly the
    # kind of residue that outlives the reason for it.
    op.execute(
        f"ALTER TABLE {_NEW_TABLE} "
        "RENAME CONSTRAINT ux_chconn_org_name TO ux_whconn_org_name"
    )
    op.execute("ALTER INDEX ux_chconn_org_default RENAME TO ux_whconn_org_default")

    op.add_column(
        _NEW_TABLE,
        sa.Column(
            "type", sa.Text(), nullable=False, server_default=sa.text("'clickhouse'")
        ),
    )
    op.add_column(
        _NEW_TABLE,
        sa.Column(
            "description", sa.Text(), nullable=False, server_default=sa.text("''")
        ),
    )
    # Postgres only; NULL means "driver default, or require when secure is set".
    op.add_column(_NEW_TABLE, sa.Column("sslmode", sa.Text(), nullable=True))

    op.create_check_constraint(
        "ck_whconn_type", _NEW_TABLE, "type IN ('clickhouse','postgres')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_whconn_type", _NEW_TABLE, type_="check")
    op.drop_column(_NEW_TABLE, "sslmode")
    op.drop_column(_NEW_TABLE, "description")
    op.drop_column(_NEW_TABLE, "type")

    op.execute("ALTER INDEX ux_whconn_org_default RENAME TO ux_chconn_org_default")
    op.execute(
        f"ALTER TABLE {_NEW_TABLE} "
        "RENAME CONSTRAINT ux_whconn_org_name TO ux_chconn_org_name"
    )
    op.rename_table(_NEW_TABLE, _OLD_TABLE)
