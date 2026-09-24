"""dashboards: remember which report template a dashboard was built from

A template dashboard (``datatalk/dashboards/templates``) is an ordinary saved
dashboard -- queries, filter templates, authoring document -- except that its
queries have no un-parameterized form: every one is written against the
template's filters. So a refresh must always bind, defaults included, and this
column is how it knows to. It also lets the UI offer the template's widget
catalog when editing.

``{}`` means "not from a template", which is every existing row, so the change
is additive and metadata-only on Postgres.

``tests/test_migrations.py`` asserts this file and the ORM models agree.

Revision ID: 0008
Revises: 0007
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dashboards",
        sa.Column(
            "template",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dashboards", "template")
