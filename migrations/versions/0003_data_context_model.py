"""context model: a per-org virtual filesystem of markdown documentation

The agents' bottleneck is not the model, it is that the only semantic metadata
reaching a prompt is one free-text description per source. This adds a curated,
per-org set of markdown files -- an entity ontology and analysis playbooks --
whose one-line summaries go into every catalog-bearing prompt and whose bodies
are fetched on demand through the ``read_context`` tool.

A filesystem rather than a nested document: ``path`` is the identity, so the
whole thing serializes to a directory of ``.md`` files and the planned git sync
is a serializer, not a schema change. ``ck_dcfile_path`` enforces the two-folder
shape in the database itself, because these paths become filenames and a
traversal must be unrepresentable rather than merely validated away.

Purely additive: two new tables, no backfill, nothing existing is touched.

``tests/test_migrations.py`` asserts this file and the ORM models agree.

Revision ID: 0003
Revises: 0002
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FILES = "data_context_files"
_DOCS = "data_context_docs"

# Kept in step with models.CONTEXT_PATH_RE / CONTEXT_ORIGINS.
_PATH_RE = r"^(overview\.md|(ontology|playbooks)/[a-z0-9][a-z0-9_-]{0,62}\.md)$"
_ORIGINS = "'agent','human','dbt','github','looker','confluence','tableau'"

_UUID = postgresql.UUID(as_uuid=True)
_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        _DOCS,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", _UUID, nullable=False),
        sa.Column("created_by_user_id", _UUID, nullable=True),
        sa.Column("model", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("generated_at", _TZ, nullable=True),
        sa.Column(
            "stats",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", _TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TZ, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        # One row per org, so this unique constraint serves every read and there
        # is no (org_id, id DESC) index to add.
        sa.UniqueConstraint("org_id", name="ux_dcdoc_org"),
    )

    op.create_table(
        _FILES,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", _UUID, nullable=False),
        sa.Column("created_by_user_id", _UUID, nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("body_md", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("generated_body_md", sa.Text(), nullable=True),
        sa.Column(
            "covers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "origin", sa.Text(), nullable=False, server_default=sa.text("'agent'")
        ),
        sa.Column("generated_at", _TZ, nullable=True),
        sa.Column("edited_at", _TZ, nullable=True),
        sa.Column("edited_by_user_id", _UUID, nullable=True),
        sa.Column("created_at", _TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TZ, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["edited_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        # Leads with org_id, so it also serves WHERE org_id = ? ORDER BY path.
        sa.UniqueConstraint("org_id", "path", name="ux_dcfile_org_path"),
        sa.CheckConstraint(f"path ~ '{_PATH_RE}'", name="ck_dcfile_path"),
        sa.CheckConstraint(f"origin IN ({_ORIGINS})", name="ck_dcfile_origin"),
    )


def downgrade() -> None:
    op.drop_table(_FILES)
    op.drop_table(_DOCS)
