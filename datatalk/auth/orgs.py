"""Org lookup, membership, and building a TenantContext from stored sources."""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datatalk.config import Settings, get_settings
from datatalk.context import SourceRef, TenantContext
from datatalk.db import models
from datatalk.warehouse import WarehouseSpec

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG_STRIP.sub("-", (name or "").lower()).strip("-")
    return slug or "org"


def unique_slug(db: Session, name: str) -> str:
    base = slugify(name)
    slug, n = base, 2
    while db.execute(
        select(models.Org.id).where(func.lower(models.Org.slug) == slug)
    ).first():
        slug, n = f"{base}-{n}", n + 1
    return slug


def get_membership(db: Session, org_id: UUID, user_id: UUID) -> models.Membership | None:
    return db.execute(
        select(models.Membership).where(
            models.Membership.org_id == org_id,
            models.Membership.user_id == user_id,
        )
    ).scalar_one_or_none()


def list_memberships(db: Session, user_id: UUID) -> list[models.Membership]:
    return list(
        db.execute(
            select(models.Membership)
            .where(models.Membership.user_id == user_id)
            .order_by(models.Membership.created_at.asc())
        ).scalars()
    )


def default_connection(db: Session, org_id: UUID) -> models.OrgWarehouseConnection | None:
    return db.execute(
        select(models.OrgWarehouseConnection).where(
            models.OrgWarehouseConnection.org_id == org_id,
            models.OrgWarehouseConnection.is_default.is_(True),
        )
    ).scalar_one_or_none()


def list_connections(db: Session, org_id: UUID) -> list[models.OrgWarehouseConnection]:
    """Every source an org has, default first then by name.

    That order is what the agent sees, and the first entry is what
    ``ctx.warehouse(None)`` resolves to.
    """
    return list(
        db.execute(
            select(models.OrgWarehouseConnection)
            .where(models.OrgWarehouseConnection.org_id == org_id)
            .order_by(
                models.OrgWarehouseConnection.is_default.desc(),
                models.OrgWarehouseConnection.name.asc(),
            )
        ).scalars()
    )


def get_connection(
    db: Session, org_id: UUID, connection_id: UUID
) -> models.OrgWarehouseConnection | None:
    """One source, scoped to the org.

    Scoped, not a bare ``db.get``: an id from another org must read as absent
    so the endpoint can 404 rather than confirm it exists.
    """
    return db.execute(
        select(models.OrgWarehouseConnection).where(
            models.OrgWarehouseConnection.id == connection_id,
            models.OrgWarehouseConnection.org_id == org_id,
        )
    ).scalar_one_or_none()


def bootstrap_from_settings(db: Session, base: Settings | None = None) -> str | None:
    """Create the configured org + owner if they are absent. Idempotent.

    Without this, a deployment with ``DATATALK_ALLOW_OPEN_SIGNUP=false`` has no
    way to create its first account: signup is closed and there is no CLI for
    it. Returns a short description of what it made, or None when unconfigured
    or when everything already existed.
    """
    from datatalk.auth import passwords

    settings = base or get_settings()
    email = (settings.bootstrap_admin_email or "").strip()
    org_name = (settings.bootstrap_org_name or "").strip()
    password = settings.bootstrap_admin_password or ""
    if not (email and org_name and password):
        return None

    made: list[str] = []

    # lower(email), matching signup and the ix_users_email_lower unique index.
    user = db.execute(
        select(models.User).where(func.lower(models.User.email) == email.lower())
    ).scalar_one_or_none()
    if user is None:
        user = models.User(
            email=email, password_hash=passwords.hash_password(password)
        )
        db.add(user)
        db.flush()
        made.append(f"user {email}")

    org = db.execute(
        select(models.Org).where(func.lower(models.Org.name) == org_name.lower())
    ).scalar_one_or_none()
    if org is None:
        org = models.Org(name=org_name, slug=unique_slug(db, org_name))
        db.add(org)
        db.flush()
        made.append(f"org {org.slug}")

    if get_membership(db, org.id, user.id) is None:
        db.add(models.Membership(org_id=org.id, user_id=user.id, role="owner"))
        db.flush()
        made.append("owner membership")

    return ", ".join(made) if made else None


def spec_from_connection(
    connection: models.OrgWarehouseConnection,
    base: Settings | None = None,
) -> WarehouseSpec:
    """Turn a stored source row into a connectable spec.

    The env supplies the ceilings a source inherits when it sets no override
    (``INTROSPECT_*``, ``SQL_*``); everything about *reaching* the warehouse
    comes from the row. Nullable columns mean "inherit", so ``or``-style
    defaulting would wrongly swallow a deliberate zero -- hence the explicit
    ``is None`` checks.

    This replaced an earlier trick of overlaying the row onto ``Settings`` as
    ``CLICKHOUSE_*`` keys, which could only ever describe one source and one
    engine.
    """
    s = base or get_settings()

    def override(value: int | None, fallback: int) -> int:
        return fallback if value is None else value

    return WarehouseSpec(
        type=connection.type,
        host=connection.host,
        port=connection.port,
        username=connection.username,
        password=connection.password or "",
        database=connection.database,
        secure=connection.secure,
        sslmode=connection.sslmode,
        introspect_databases=tuple(connection.introspect_databases or ()),
        introspect_tables=tuple(
            t.lower() for t in (connection.introspect_tables or ())
        ),
        introspect_exclude_patterns=tuple(
            p.lower() for p in (connection.introspect_exclude_patterns or ())
        ),
        introspect_sample_rows=override(
            connection.introspect_sample_rows, s.introspect_sample_rows
        ),
        introspect_max_tables=override(
            connection.introspect_max_tables, s.introspect_max_tables
        ),
        sql_default_limit=override(connection.sql_default_limit, s.sql_default_limit),
        sql_max_rows=override(connection.sql_max_rows, s.sql_max_rows),
        sql_timeout_seconds=override(
            connection.sql_timeout_seconds, s.sql_timeout_seconds
        ),
    )


def source_ref(
    connection: models.OrgWarehouseConnection, base: Settings | None = None
) -> SourceRef:
    return SourceRef.from_spec(
        spec_from_connection(connection, base),
        id=connection.id,
        name=connection.name,
        description=connection.description or "",
        is_default=connection.is_default,
    )


def build_tenant_context(
    db: Session,
    *,
    org: models.Org,
    user: models.User | None,
    role: str,
) -> TenantContext:
    """The one place a request-scoped TenantContext is constructed.

    Loads *every* source the org has -- the agent chooses between them per
    query, so there is no selection to make here. An org with none gets an
    empty tuple, which is what makes ``has_connection`` false and leaves no
    path to the deployment's own warehouse.

    The context model is loaded here too, for the same reason the sources are:
    this is the last point that holds a session. The agent worker threads that
    read it have none.
    """
    from datatalk.memory.datacontext import load_context

    base = get_settings()
    sources = tuple(source_ref(c, base) for c in list_connections(db, org.id))
    return TenantContext.from_sources(
        sources,
        org_id=org.id,
        org_slug=org.slug,
        user_id=user.id if user else None,
        user_email=user.email if user else "",
        role=role,
        settings=base,
        context_model=load_context(db, org.id),
    )
