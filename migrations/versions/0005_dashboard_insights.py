"""dashboards: persist the insight pass's structured findings

The insight pass reviews the full captured data and returns structured
findings that steer which widgets the author builds and in what order. Until
now they existed only as a transient stream event: the UI discarded them and
nothing stored them, so a saved dashboard could not explain why it leads with
what it leads with. One JSONB column beside ``document``/``queries``.

Purely additive: the empty-object default reproduces today's behaviour for
every existing dashboard, no backfill needed. Metadata-only on Postgres (a
non-volatile default on a new column rewrites no rows).

``tests/test_migrations.py`` asserts this file and the ORM models agree.

Revision ID: 0005
Revises: 0004
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dashboards",
        sa.Column(
            "insights",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dashboards", "insights")
