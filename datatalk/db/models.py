"""SQLAlchemy models: identity, per-org data sources, and org-scoped content.

Two id conventions, deliberately mixed:

* **Identity tables use UUIDs.** Org and user ids appear in URLs and cookies;
  unguessable ids are worth the cost there.
* **Content tables keep integer ids.** The frontend does ``parseInt(id)`` and
  routes declare ``int`` path params. Switching those to UUIDs would ripple
  through the UI for no security gain: cross-tenant access is prevented by the
  ``org_id`` filter, not by id unguessability.

Every tenant table carries ``org_id NOT NULL``, which also leaves the schema
ready for Postgres row-level security if we ever want defense in depth. We do
not enable RLS now (single app role, per-transaction GUCs, extra failure modes),
but nothing here would need redesigning to add it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from datatalk.db.types import EncryptedStr

_UUID_PK = PgUUID(as_uuid=True)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[UUID]:
    return mapped_column(
        _UUID_PK, primary_key=True, server_default=text("gen_random_uuid()")
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# --- identity -----------------------------------------------------------------


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="org", cascade="all, delete-orphan"
    )
    connections: Mapped[list["OrgClickHouseConnection"]] = relationship(
        back_populates="org", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_orgs_slug_lower", func.lower(slug), unique=True),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    # argon2id. Never Fernet -- password verification must be one-way.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Lockout state lives in columns, not an in-process dict: a dict is useless
    # the moment more than one uvicorn worker runs.
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_email_lower", func.lower(email), unique=True),
    )


class Membership(Base):
    """Users are global; this join is what scopes them to an org."""

    __tablename__ = "memberships"

    org_id: Mapped[UUID] = mapped_column(
        _UUID_PK, ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        _UUID_PK, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'member'"))
    created_at: Mapped[datetime] = _created_at()

    org: Mapped[Org] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")

    __table_args__ = (
        # A CHECK rather than a native ENUM: adding an ENUM value in Postgres is
        # a non-transactional ALTER TYPE that Alembic handles badly.
        CheckConstraint(
            "role IN ('owner','admin','member','viewer')", name="ck_memberships_role"
        ),
        Index("ix_memberships_user", "user_id"),
    )


class AuthSession(Base):
    """Opaque server-side session. Not a JWT -- revocation must be immediate.

    ``id`` is the sha256 of the cookie value, never the value itself, so a
    leaked database dump cannot be replayed as a login.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        _UUID_PK, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Which org this session is currently acting as. SET NULL so deleting an org
    # forces a re-pick rather than logging everyone out.
    current_org_id: Mapped[UUID | None] = mapped_column(
        _UUID_PK, ForeignKey("orgs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)

    user: Mapped[User] = relationship()

    __table_args__ = (
        Index("ix_auth_sessions_user", "user_id"),
        Index("ix_auth_sessions_expires", "expires_at"),
    )


# --- per-org data source ------------------------------------------------------


class OrgClickHouseConnection(Base):
    """An org's ClickHouse credentials and introspection scope.

    A separate table rather than columns on ``orgs``: one place for every
    secret, one access path, and room for multiple named connections later
    without a data migration.
    """

    __tablename__ = "org_clickhouse_connections"

    id: Mapped[UUID] = _uuid_pk()
    org_id: Mapped[UUID] = mapped_column(
        _UUID_PK, ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("8123"))
    username: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    # Encrypted by the column type; there is no path that writes it in plaintext.
    password: Mapped[str | None] = mapped_column("password_encrypted", EncryptedStr)
    database: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    secure: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # Per-org overrides of the INTROSPECT_*/SQL_* env globals. NULL = inherit.
    introspect_databases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    introspect_exclude_patterns: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    introspect_sample_rows: Mapped[int | None] = mapped_column(Integer)
    introspect_max_tables: Mapped[int | None] = mapped_column(Integer)
    sql_default_limit: Mapped[int | None] = mapped_column(Integer)
    sql_max_rows: Mapped[int | None] = mapped_column(Integer)
    sql_timeout_seconds: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        _UUID_PK, ForeignKey("users.id", ondelete="SET NULL")
    )

    org: Mapped[Org] = relationship(back_populates="connections")

    __table_args__ = (
        UniqueConstraint("org_id", "name", name="ux_chconn_org_name"),
        # Exactly one default per org, enforced by the database.
        Index(
            "ux_chconn_org_default",
            "org_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    def __repr__(self) -> str:  # never render the password
        return (
            f"<OrgClickHouseConnection org={self.org_id} name={self.name!r} "
            f"host={self.host!r} db={self.database!r}>"
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Safe for API responses: reports whether a password exists, never it."""
        return {
            "configured": True,
            "host": self.host,
            "port": self.port,
            "user": self.username,
            "database": self.database,
            "secure": self.secure,
            "has_password": bool(self.password),
            "introspect_databases": list(self.introspect_databases or []),
            "introspect_exclude_patterns": list(self.introspect_exclude_patterns or []),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# --- org-scoped content -------------------------------------------------------
#
# Every table below carries the same two columns:
#   org_id             -> CASCADE  (deleting an org removes its data)
#   created_by_user_id -> SET NULL (deleting a user must not delete org content)


def _org_fk() -> Mapped[UUID]:
    return mapped_column(
        _UUID_PK, ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )


def _author_fk() -> Mapped[UUID | None]:
    return mapped_column(_UUID_PK, ForeignKey("users.id", ondelete="SET NULL"))


class Suggestion(Base):
    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[UUID] = _org_fk()
    created_by_user_id: Mapped[UUID | None] = _author_fk()
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    # float32 little-endian, same bytes the previous SQLite BLOB held.
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Recording these is a correctness fix, not bookkeeping: mixing vectors from
    # two embedding models makes the dot product raise and silently kills ALL
    # retrieval, because the web layer swallows retrieval errors.
    embedding_dim: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    legacy_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        # (org_id, id DESC) serves filter + sort + limit from one index scan;
        # every list query is WHERE org_id = ? ORDER BY id DESC LIMIT n.
        Index("ix_suggestions_org", "org_id", text("id DESC")),
        Index(
            "ux_suggestions_legacy",
            "org_id",
            "legacy_id",
            unique=True,
            postgresql_where=text("legacy_id IS NOT NULL"),
        ),
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[UUID] = _org_fk()
    created_by_user_id: Mapped[UUID | None] = _author_fk()
    request: Mapped[str] = mapped_column(Text, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    queries: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    legacy_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        # Target for the composite FK on qa_turns below.
        UniqueConstraint("id", "org_id", name="uq_reports_id_org"),
        Index("ix_reports_org", "org_id", text("id DESC")),
        Index(
            "ux_reports_legacy",
            "org_id",
            "legacy_id",
            unique=True,
            postgresql_where=text("legacy_id IS NOT NULL"),
        ),
    )


class QATurn(Base):
    __tablename__ = "qa_turns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[UUID] = mapped_column(_UUID_PK, nullable=False)
    report_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by_user_id: Mapped[UUID | None] = _author_fk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_document: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    queries: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    legacy_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        # The most valuable constraint in this schema: it is structurally
        # impossible to attach a Q&A turn to another org's report, whatever the
        # application code does. Costs one extra unique index on reports.
        ForeignKeyConstraint(
            ["report_id", "org_id"],
            ["reports.id", "reports.org_id"],
            ondelete="CASCADE",
            name="fk_qa_report",
        ),
        Index("ix_qa_turns_report", "report_id", "id"),  # absent in the old schema
        Index("ix_qa_turns_org", "org_id"),
    )


class Dashboard(Base):
    __tablename__ = "dashboards"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[UUID] = _org_fk()
    created_by_user_id: Mapped[UUID | None] = _author_fk()
    request: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    document: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    queries: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    analysis: Mapped[str | None] = mapped_column(Text)
    legacy_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        Index("ix_dashboards_org", "org_id", text("id DESC")),
        Index(
            "ux_dashboards_legacy",
            "org_id",
            "legacy_id",
            unique=True,
            postgresql_where=text("legacy_id IS NOT NULL"),
        ),
    )
