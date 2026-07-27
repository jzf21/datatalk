"""source scope: a per-namespace table allowlist beside the database allowlist

``introspect_databases`` could already narrow a source to some of the databases
on its server, but not to some of the *tables* in a database. On a ClickHouse
server holding a dozen databases that is the difference between a catalog the
model can reason about and one that fills the prompt with staging and backup
tables it then confuses for the real ones.

One flat ``text[]`` of qualified ``namespace.table`` entries rather than a
JSONB map, matching the two array columns it sits beside. The semantics are
per namespace: a namespace named in this list shows only the tables named for
it, a namespace absent from it shows all of its tables. That keeps "the whole
billing database" and "three tables from analytics" in one column, and lets a
whole-database selection keep picking up tables created later.

Purely additive: the empty default reproduces today's behaviour exactly, so
every existing source is unaffected and no backfill is needed.

``tests/test_migrations.py`` asserts this file and the ORM models agree.

Revision ID: 0004
Revises: 0003
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "org_warehouse_connections"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "introspect_tables",
            sa.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "introspect_tables")
