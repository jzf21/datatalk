"""dashboards: keep the authoring document, and per-dashboard filters

A saved dashboard was a frozen snapshot: ``document`` holds materialized values,
and ``GET /api/dashboards/{id}`` never touches a warehouse, so reloading showed
whatever the numbers were when the dashboard was generated.

Re-running the captured SQL is not enough to fix that on its own, because the
document cannot be reversed into the form that re-materialization needs:
``_materialize_stat`` keeps ``value``/``delta`` but discards ``value_col``,
``row_index`` and ``delta_col``, and nothing left on the block says which column
it read. So the pre-materialization document is persisted alongside it, and
refresh materializes *that* against freshly executed datasets -- the same call
the generation pipeline makes, minus the LLM.

``filters`` carries the per-dashboard filter definitions and the placeholder-
bearing SQL templates they bind into. One column rather than two: a template
exists only because a filter binds to that dataset, and the same request writes
both, so splitting them would only create a way for them to disagree.

Purely additive, and both defaults are the "no such thing configured" value, so
every existing dashboard keeps behaving exactly as it does today -- an empty
``authoring_document`` is precisely how a row says "generated before this
shipped, refresh me best-effort". Metadata-only on Postgres: a non-volatile
default on a new column rewrites no rows.

``tests/test_migrations.py`` asserts this file and the ORM models agree.

Revision ID: 0006
Revises: 0005
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dashboards",
        sa.Column(
            "authoring_document",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "dashboards",
        sa.Column(
            "filters",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dashboards", "filters")
    op.drop_column("dashboards", "authoring_document")
