"""SQLAlchemy models: identity, per-org data sources, and org-scoped content.

An org holds any number of named data sources (``OrgWarehouseConnection``),
each a ClickHouse or Postgres warehouse. The agent sees all of them at once and
picks per query, so nothing here records "the" connection for a report -- query
provenance is recorded per captured dataset in ``reports.queries``.

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
    connections: Mapped[list["OrgWarehouseConnection"]] = relationship(
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


class OrgWarehouseConnection(Base):
    """One of an org's data sources: credentials, engine, and introspection scope.

    A separate table rather than columns on ``orgs``: one place for every
    secret, one access path, and room for the several named sources an org
    actually has.

    ``name`` is not cosmetic. It is the handle the LLM types in
    ``run_sql(source=...)``, so it is unique per org and constrained to a plain
    identifier by the API layer. ``description`` is likewise load-bearing: it
    goes into the schema catalog and is the main signal the model uses to pick
    the right source for a question.
    """

    __tablename__ = "org_warehouse_connections"

    id: Mapped[UUID] = _uuid_pk()
    org_id: Mapped[UUID] = mapped_column(
        _UUID_PK, ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'clickhouse'"))
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))

    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("8123"))
    username: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    # Encrypted by the column type; there is no path that writes it in plaintext.
    password: Mapped[str | None] = mapped_column("password_encrypted", EncryptedStr)
    database: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'default'"))
    secure: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Postgres only: disable/allow/require/verify-ca/verify-full. NULL = driver
    # default, or "require" when ``secure`` is set.
    sslmode: Mapped[str | None] = mapped_column(Text)

    # Per-source overrides of the INTROSPECT_*/SQL_* env globals. NULL = inherit.
    # On ClickHouse ``introspect_databases`` is a database allowlist; on
    # Postgres one connection sees one database, so it is a schema allowlist.
    introspect_databases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    # Qualified ``namespace.table`` entries, scoped per namespace: a namespace
    # named here shows only its listed tables, one absent from it shows all of
    # them. Empty behaves exactly as before this column existed.
    introspect_tables: Mapped[list[str]] = mapped_column(
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
        UniqueConstraint("org_id", "name", name="ux_whconn_org_name"),
        # A CHECK rather than a native ENUM, for the reason given on
        # ck_memberships_role above.
        CheckConstraint(
            "type IN ('clickhouse','postgres')", name="ck_whconn_type"
        ),
        # At most one default per org, enforced by the database.
        Index(
            "ux_whconn_org_default",
            "org_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    def __repr__(self) -> str:  # never render the password
        return (
            f"<OrgWarehouseConnection org={self.org_id} name={self.name!r} "
            f"type={self.type!r} host={self.host!r} db={self.database!r}>"
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Safe for API responses: reports whether a password exists, never it."""
        return {
            "id": str(self.id),
            "name": self.name,
            "type": self.type,
            "description": self.description or "",
            "is_default": self.is_default,
            "configured": True,
            "host": self.host,
            "port": self.port,
            "user": self.username,
            "database": self.database,
            "secure": self.secure,
            "sslmode": self.sslmode,
            "has_password": bool(self.password),
            "introspect_databases": list(self.introspect_databases or []),
            "introspect_tables": list(self.introspect_tables or []),
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
    # The insight pass's structured findings ({"insights": [...], ...}). The
    # grid is shaped by them, so a saved dashboard without them loses the
    # "why these widgets" part of its own story.
    insights: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # The document *before* materialization -- blocks that reference a dataset by
    # id and column instead of holding values. This is what makes a dashboard
    # refreshable: `document` cannot be reversed, because materializing a stat
    # discards the column it read (see agent/blocks.py `dematerialize`). Empty
    # `{}` marks a dashboard saved before this column existed; that emptiness IS
    # the refreshability predicate, so no second flag column is needed.
    authoring_document: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Per-dashboard filter definitions plus the per-dataset SQL templates they
    # bind into. One column because a template exists only because a filter binds
    # to that dataset, and the same request writes both.
    filters: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
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


# --- the context model --------------------------------------------------------
#
# Curated documentation of what an org's data *means*, as opposed to what it
# contains. The catalog tells the agent that `orders.status` exists; only this
# tells it that 'void' must be excluded from revenue. Summaries go into every
# catalog-bearing prompt; bodies are fetched on demand via the `read_context`
# tool. See datatalk/memory/datacontext.py.


# A context path becomes a filename on git sync, so traversal and absolute paths
# must be unrepresentable rather than merely rejected by a validator. Shared with
# the migration and the request models so the three cannot drift.
CONTEXT_PATH_RE = r"^(overview\.md|(ontology|playbooks)/[a-z0-9][a-z0-9_-]{0,62}\.md)$"

CONTEXT_ORIGINS = ("agent", "human", "dbt", "github", "looker", "confluence", "tableau")


class DataContextFile(Base):
    """One markdown file in an org's context model.

    A virtual filesystem, not a nested document: ``path`` is the identity, and
    the table serializes losslessly to a directory of ``.md`` files (see
    ``memory/datacontext.to_markdown``). That is what keeps a future git sync a
    serializer rather than a schema change.

    ``ontology/`` files are keyed by *business entity*, not by table -- one
    entity may span several tables and even several sources -- so ``covers``
    records the tables a file documents rather than the path encoding them.

    Two deliberate departures from the content-table template above: no
    ``legacy_id`` (``datatalk-import-sqlite`` predates this feature and will
    never write a context file, so the column would be permanently dead), and
    ``origin`` exists so a future dbt/GitHub importer can own a file that the
    warehouse generator must not clobber.
    """

    __tablename__ = "data_context_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[UUID] = _org_fk()
    created_by_user_id: Mapped[UUID | None] = _author_fk()

    # "overview.md" | "ontology/<slug>.md" | "playbooks/<slug>.md"
    path: Mapped[str] = mapped_column(Text, nullable=False)
    # The one line that goes into EVERY prompt. This is the always-on token
    # budget, which is why it is a column and not parsed out of the body.
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    body_md: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # The last body the generator wrote -- the merge base. body_md differing from
    # this is what "a human owns this file" means. NULL for a hand-created file.
    generated_body_md: Mapped[str | None] = mapped_column(Text)

    # [{"source": "main", "table": "analytics.orders"}] -- lets describe_source
    # attach the covering ontology file with no extra round trip.
    covers: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # [{"source": ..., "sql": ..., "row_count": ...}] -- the profiling queries
    # behind the claims, so "why does it say status can be 'void'" is answerable.
    evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    origin: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'agent'")
    )

    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edited_by_user_id: Mapped[UUID | None] = _author_fk()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        # Leads with org_id, so it also serves the cache-fill query
        # WHERE org_id = ? ORDER BY path. No separate index needed.
        UniqueConstraint("org_id", "path", name="ux_dcfile_org_path"),
        CheckConstraint(f"path ~ '{CONTEXT_PATH_RE}'", name="ck_dcfile_path"),
        CheckConstraint(
            "origin IN ('agent','human','dbt','github','looker','confluence','tableau')",
            name="ck_dcfile_origin",
        ),
    )


class DataContextDoc(Base):
    """Last-run metadata for an org's context model. One row per org.

    Separate from the files so a hand-written file can exist before any
    generation run. No ``(org_id, id DESC)`` index: with one row per org the
    unique constraint serves every read.
    """

    __tablename__ = "data_context_docs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[UUID] = _org_fk()
    created_by_user_id: Mapped[UUID | None] = _author_fk()
    # Which model wrote it -- worth knowing when the output disappoints.
    model: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # {"sources": [...], "tables_seen": 312, "tables_profiled": 64,
    #  "entities": 9, "playbooks": 5, "queries": 41, "truncated": false}
    stats: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (UniqueConstraint("org_id", name="ux_dcdoc_org"),)
