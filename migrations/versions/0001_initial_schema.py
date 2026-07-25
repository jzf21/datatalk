"""initial multi-tenant schema

Hand-written rather than pure autogenerate: Alembic's autogenerate does not
emit partial unique indexes (``WHERE is_default``, ``WHERE legacy_id IS NOT
NULL``), functional indexes (``lower(email)``), or a composite foreign key
targeting a non-primary unique constraint -- which is precisely the set of
constraints that enforce tenant isolation here.

``tests/test_migrations.py`` asserts this file and the ORM models agree.

Revision ID: 0001
Revises:
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UUID = postgresql.UUID(as_uuid=True)
_TZ = sa.DateTime(timezone=True)


def _created_at(name: str = "created_at") -> sa.Column:
    return sa.Column(name, _TZ, nullable=False, server_default=sa.text("now()"))


def _org_fk() -> sa.Column:
    return sa.Column(
        "org_id",
        _UUID,
        sa.ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
    )


def _author_fk() -> sa.Column:
    return sa.Column(
        "created_by_user_id",
        _UUID,
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


def _content_indexes(table: str) -> None:
    """(org_id, id DESC) serves filter+sort+limit for list queries; the partial
    unique on legacy_id makes the SQLite import idempotent."""
    op.create_index(f"ix_{table}_org", table, ["org_id", sa.text("id DESC")])
    op.create_index(
        f"ux_{table}_legacy",
        table,
        ["org_id", "legacy_id"],
        unique=True,
        postgresql_where=sa.text("legacy_id IS NOT NULL"),
    )


def upgrade() -> None:
    # gen_random_uuid() is built into PG13+; no pgcrypto extension needed.

    # --- identity ---
    op.create_table(
        "orgs",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        _created_at(),
    )
    op.create_index("ix_orgs_slug_lower", "orgs", [sa.text("lower(slug)")], unique=True)

    op.create_table(
        "users",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", _TZ, nullable=True),
        _created_at(),
        sa.Column("last_login_at", _TZ, nullable=True),
    )
    op.create_index("ix_users_email_lower", "users", [sa.text("lower(email)")], unique=True)

    op.create_table(
        "memberships",
        sa.Column("org_id", _UUID, sa.ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'member'")),
        _created_at(),
        sa.CheckConstraint(
            "role IN ('owner','admin','member','viewer')", name="ck_memberships_role"
        ),
    )
    op.create_index("ix_memberships_user", "memberships", ["user_id"])

    op.create_table(
        "auth_sessions",
        # sha256 hex of the cookie value, never the value itself.
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", _UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "current_org_id", _UUID, sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
        ),
        _created_at(),
        _created_at("last_seen_at"),
        sa.Column("expires_at", _TZ, nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
    )
    op.create_index("ix_auth_sessions_user", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires", "auth_sessions", ["expires_at"])

    # --- per-org data source ---
    op.create_table(
        "org_clickhouse_connections",
        sa.Column("id", _UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", _UUID, sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default=sa.text("'default'")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default=sa.text("8123")),
        sa.Column("username", sa.Text(), nullable=False, server_default=sa.text("'default'")),
        sa.Column("password_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("database", sa.Text(), nullable=False, server_default=sa.text("'default'")),
        sa.Column("secure", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "introspect_databases",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "introspect_exclude_patterns",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("introspect_sample_rows", sa.Integer(), nullable=True),
        sa.Column("introspect_max_tables", sa.Integer(), nullable=True),
        sa.Column("sql_default_limit", sa.Integer(), nullable=True),
        sa.Column("sql_max_rows", sa.Integer(), nullable=True),
        sa.Column("sql_timeout_seconds", sa.Integer(), nullable=True),
        _created_at(),
        sa.Column("updated_at", _TZ, nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "updated_by_user_id", _UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.UniqueConstraint("org_id", "name", name="ux_chconn_org_name"),
    )
    # Exactly one default connection per org, enforced by the database.
    op.create_index(
        "ux_chconn_org_default",
        "org_clickhouse_connections",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    # --- org-scoped content ---
    op.create_table(
        "suggestions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _org_fk(),
        _author_fk(),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("embedding_dim", sa.SmallInteger(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("legacy_id", sa.BigInteger(), nullable=True),
        _created_at(),
    )
    _content_indexes("suggestions")

    op.create_table(
        "reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _org_fk(),
        _author_fk(),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column(
            "document", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "queries", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("legacy_id", sa.BigInteger(), nullable=True),
        _created_at(),
        # Target for the composite FK from qa_turns.
        sa.UniqueConstraint("id", "org_id", name="uq_reports_id_org"),
    )
    _content_indexes("reports")

    op.create_table(
        "qa_turns",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", _UUID, nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        _author_fk(),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "answer_document",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "queries", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("legacy_id", sa.BigInteger(), nullable=True),
        _created_at(),
        # Makes cross-org Q&A attachment structurally impossible.
        sa.ForeignKeyConstraint(
            ["report_id", "org_id"],
            ["reports.id", "reports.org_id"],
            ondelete="CASCADE",
            name="fk_qa_report",
        ),
    )
    op.create_index("ix_qa_turns_report", "qa_turns", ["report_id", "id"])
    op.create_index("ix_qa_turns_org", "qa_turns", ["org_id"])

    op.create_table(
        "dashboards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _org_fk(),
        _author_fk(),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "document", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "queries", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("analysis", sa.Text(), nullable=True),
        sa.Column("legacy_id", sa.BigInteger(), nullable=True),
        _created_at(),
    )
    _content_indexes("dashboards")


def downgrade() -> None:
    for table in (
        "dashboards",
        "qa_turns",
        "reports",
        "suggestions",
        "org_clickhouse_connections",
        "auth_sessions",
        "memberships",
        "users",
        "orgs",
    ):
        op.drop_table(table)
