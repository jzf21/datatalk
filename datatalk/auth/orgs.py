"""Org lookup, membership, and building a TenantContext from stored credentials."""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datatalk.config import Settings, get_settings
from datatalk.context import TenantContext
from datatalk.db import models

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


def default_connection(db: Session, org_id: UUID) -> models.OrgClickHouseConnection | None:
    return db.execute(
        select(models.OrgClickHouseConnection).where(
            models.OrgClickHouseConnection.org_id == org_id,
            models.OrgClickHouseConnection.is_default.is_(True),
        )
    ).scalar_one_or_none()


def effective_settings(
    connection: models.OrgClickHouseConnection | None,
    base: Settings | None = None,
) -> Settings:
    """Overlay an org's stored connection onto the environment defaults.

    The env provides everything not org-specific (OpenAI credentials, guardrail
    ceilings); the org row overrides the ClickHouse target and, where set, the
    introspection and SQL limits. Returns the env settings untouched when the
    org has no connection yet.
    """
    base = base or get_settings()
    if connection is None:
        return base

    overrides: dict[str, object] = {
        "CLICKHOUSE_HOST": connection.host,
        "CLICKHOUSE_PORT": connection.port,
        "CLICKHOUSE_USER": connection.username,
        "CLICKHOUSE_PASSWORD": connection.password or "",
        "CLICKHOUSE_DATABASE": connection.database,
        "CLICKHOUSE_SECURE": connection.secure,
        "INTROSPECT_DATABASES": ",".join(connection.introspect_databases or []),
        "INTROSPECT_EXCLUDE_TABLE_PATTERNS": ",".join(
            connection.introspect_exclude_patterns or []
        ),
    }
    optional = {
        "INTROSPECT_SAMPLE_ROWS": connection.introspect_sample_rows,
        "INTROSPECT_MAX_TABLES": connection.introspect_max_tables,
        "SQL_DEFAULT_LIMIT": connection.sql_default_limit,
        "SQL_MAX_ROWS": connection.sql_max_rows,
        "SQL_TIMEOUT_SECONDS": connection.sql_timeout_seconds,
    }
    overrides.update({k: v for k, v in optional.items() if v is not None})

    # Start from the env values so unrelated settings (OpenAI, pool sizes) are
    # preserved, then apply the org's overrides on top.
    merged = base.model_dump(by_alias=True)
    merged.update(overrides)
    return Settings(**merged)


def build_tenant_context(
    db: Session,
    *,
    org: models.Org,
    user: models.User | None,
    role: str,
) -> TenantContext:
    """The one place a request-scoped TenantContext is constructed."""
    settings = effective_settings(default_connection(db, org.id))
    return TenantContext.from_settings(
        settings,
        org_id=org.id,
        org_slug=org.slug,
        user_id=user.id if user else None,
        user_email=user.email if user else "",
        role=role,
    )
