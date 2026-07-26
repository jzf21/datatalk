"""Org membership and per-org data source management.

An org holds any number of named sources, each ClickHouse or Postgres. The
agent picks between them per query, so there is no "current" source to switch:
these routes are plain CRUD over the collection.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from datatalk import clients, warehouse
from datatalk.auth import orgs as orgs_svc
from datatalk.auth import sessions as sessions_svc
from datatalk.db import models
from datatalk.memory.datacontext import invalidate_context
from datatalk.warehouse import catalog
from datatalk.web.deps import (
    RequestContext,
    csrf_guard,
    get_auth_session,
    get_current_user,
    get_db,
    get_request_ctx,
    require_admin,
    require_same_org,
)

# Router-level dependencies, so a route added here cannot forget either guard.
router = APIRouter(
    prefix="/api/orgs",
    tags=["orgs"],
    dependencies=[Depends(get_current_user), Depends(csrf_guard)],
)


class CreateOrgRequest(BaseModel):
    name: str


class _ConnectionBase(BaseModel):
    # The handle the LLM types in run_sql(source=...), so it is a plain
    # identifier: no spaces or punctuation for the model to mangle.
    name: str = Field(
        default="default", pattern=r"^[a-z][a-z0-9_]{0,39}$", max_length=40
    )
    # Free text describing what lives here. Goes into the schema catalog and is
    # the main signal the model uses to route a question to the right source.
    description: str = Field(default="", max_length=500)
    is_default: bool = False
    host: str
    user: str
    # Omitted or null means "keep the stored password" -- the UI never receives
    # the current one, so it cannot echo it back.
    password: str | None = None
    database: str
    secure: bool = False
    introspect_databases: list[str] | None = None
    introspect_exclude_patterns: list[str] | None = None


class ClickHouseConnectionRequest(_ConnectionBase):
    type: Literal["clickhouse"] = "clickhouse"
    port: int = Field(default=8123, ge=1, le=65535)
    user: str = "default"
    database: str = "default"


class PostgresConnectionRequest(_ConnectionBase):
    type: Literal["postgres"] = "postgres"
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = "postgres"
    database: str = "postgres"
    sslmode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] | None = None


# A discriminated union rather than one model with a `type` field: the port,
# user and database defaults genuinely differ per engine, and Postgres alone
# has sslmode.
ConnectionRequest = Annotated[
    Union[ClickHouseConnectionRequest, PostgresConnectionRequest],
    Field(discriminator="type"),
]


# Both guards live in web/deps.py, beside the error-code table they implement,
# so routes_datacontext can reuse them without importing this module.
_require_admin = require_admin
_require_same_org = require_same_org


@router.get("")
def list_orgs(
    user: models.User = Depends(get_current_user),
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    out = []
    for membership in orgs_svc.list_memberships(db, user.id):
        org = db.get(models.Org, membership.org_id)
        out.append(
            {
                "id": str(org.id),
                "slug": org.slug,
                "name": org.name,
                "role": membership.role,
                "is_current": org.id == rctx.tenant.org_id,
            }
        )
    return {"orgs": out}


@router.post("", status_code=201)
def create_org(
    req: CreateOrgRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Org name is required.")

    org = models.Org(name=name, slug=orgs_svc.unique_slug(db, name))
    db.add(org)
    db.flush()
    db.add(models.Membership(org_id=org.id, user_id=user.id, role="owner"))
    db.flush()
    return {"org": {"id": str(org.id), "slug": org.slug, "name": org.name, "role": "owner"}}


@router.post("/{org_id}/switch")
def switch_org(
    org_id: UUID,
    auth_session: models.AuthSession | None = Depends(get_auth_session),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    membership = orgs_svc.get_membership(db, org_id, user.id)
    if membership is None:
        raise HTTPException(status_code=403, detail="not_a_member")

    sessions_svc.set_current_org(db, auth_session, org_id)
    org = db.get(models.Org, org_id)
    return {
        "org": {"id": str(org.id), "slug": org.slug, "name": org.name, "role": membership.role}
    }


@router.get("/{org_id}/connections")
def list_connections(
    org_id: UUID,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_same_org(rctx, org_id)
    return {
        # never includes any password
        "connections": [
            c.to_public_dict() for c in orgs_svc.list_connections(db, org_id)
        ]
    }


@router.post("/{org_id}/connections", status_code=201)
def create_connection(
    org_id: UUID,
    req: ConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    existing = orgs_svc.list_connections(db, org_id)
    if any(c.name == req.name for c in existing):
        raise HTTPException(status_code=409, detail="duplicate_source_name")

    connection = models.OrgWarehouseConnection(org_id=org_id, host=req.host)
    db.add(connection)
    _apply(connection, req, rctx)
    db.flush()
    # The first source an org adds is its default whatever the request says --
    # ctx.warehouse(None) has to resolve to something.
    if req.is_default or not existing:
        _set_default(connection, db)

    _invalidate(org_id, rctx.tenant, db)
    return connection.to_public_dict()


@router.put("/{org_id}/connections/{connection_id}")
def update_connection(
    org_id: UUID,
    connection_id: UUID,
    req: ConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    connection = orgs_svc.get_connection(db, org_id, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    clash = [
        c
        for c in orgs_svc.list_connections(db, org_id)
        if c.name == req.name and c.id != connection.id
    ]
    if clash:
        raise HTTPException(status_code=409, detail="duplicate_source_name")

    _apply(connection, req, rctx)
    db.flush()
    if req.is_default:
        _set_default(connection, db)

    _invalidate(org_id, rctx.tenant, db)
    return connection.to_public_dict()


@router.post("/{org_id}/connections/test")
def test_connection(
    org_id: UUID,
    req: ConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Try a candidate source without saving or caching it.

    Returns 200 with ``ok: false`` on failure: a failed *test* is a successful
    API call, and making the UI parse a 502 to show "wrong password" is worse.
    """
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    password = req.password
    if password is None:
        # Editing an existing source without retyping its password: reuse the
        # stored one, matched by name since the candidate has no id yet.
        stored = next(
            (c for c in orgs_svc.list_connections(db, org_id) if c.name == req.name),
            None,
        )
        password = stored.password if stored else ""

    candidate = models.OrgWarehouseConnection(
        org_id=org_id,
        name=req.name,
        type=req.type,
        host=req.host,
        port=req.port,
        username=req.user,
        password=password,
        database=req.database,
        secure=req.secure,
        sslmode=getattr(req, "sslmode", None),
        introspect_databases=req.introspect_databases or [],
        introspect_exclude_patterns=req.introspect_exclude_patterns or [],
    )
    spec = orgs_svc.spec_from_connection(candidate)

    wh = None
    try:
        # Deliberately a throwaway warehouse, never the registry: a failed test
        # must not poison the live entry for this org.
        wh = warehouse.create(spec)
        info = wh.ping()
        tables = wh.introspect(with_samples=False)
        return {
            "ok": True,
            "version": info["version"],
            "database": info["database"],
            "table_count": len(tables),
        }
    except Exception as exc:  # noqa: BLE001 - surface any driver error to the UI
        return {"ok": False, "error": str(exc)}
    finally:
        if wh is not None:
            wh.close()


@router.delete("/{org_id}/connections/{connection_id}", status_code=204)
def delete_connection(
    org_id: UUID,
    connection_id: UUID,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> None:
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    connection = orgs_svc.get_connection(db, org_id, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="connection_not_found")

    was_default = connection.is_default
    db.delete(connection)
    db.flush()

    if was_default:
        # Something must remain resolvable as the default, or every query that
        # names no source starts failing.
        remaining = orgs_svc.list_connections(db, org_id)
        if remaining:
            remaining[0].is_default = True
            db.flush()

    _invalidate(org_id, rctx.tenant, db)


def _apply(
    connection: models.OrgWarehouseConnection,
    req: Any,
    rctx: RequestContext,
) -> None:
    """Copy a validated request onto a row. Does not touch ``is_default``."""
    # A new row leaves is_default as None until the column's server default
    # (true) applies at flush -- which violates the one-default-per-org partial
    # unique index before _set_default gets a chance to demote the old one.
    if connection.is_default is None:
        connection.is_default = False

    connection.name = req.name
    connection.description = req.description
    connection.type = req.type
    connection.host = req.host
    connection.port = req.port
    connection.username = req.user
    connection.database = req.database
    connection.secure = req.secure
    connection.sslmode = getattr(req, "sslmode", None)
    if req.password is not None:
        connection.password = req.password  # encrypted by the column type
    if req.introspect_databases is not None:
        connection.introspect_databases = req.introspect_databases
    if req.introspect_exclude_patterns is not None:
        connection.introspect_exclude_patterns = req.introspect_exclude_patterns
    connection.updated_by_user_id = rctx.user.id


def _set_default(connection: models.OrgWarehouseConnection, db: Session) -> None:
    """Promote one source, demoting the rest.

    Demote-then-flush-then-promote: a partial unique index enforces one default
    per org, so setting the new one first would violate it mid-transaction.
    """
    for other in orgs_svc.list_connections(db, connection.org_id):
        if other.id != connection.id and other.is_default:
            other.is_default = False
    db.flush()
    connection.is_default = True
    db.flush()


def _invalidate(org_id: UUID, tenant, db: Session) -> None:
    """Drop cached clients and schema for an org whose sources changed.

    Without this the org keeps querying its old warehouse, and keeps seeing the
    old schema context, until the process restarts. ``tenant`` is the context
    built *before* the mutation, so its per-source fingerprints are the stale
    registry keys -- every one is dropped, since a rename or a default change
    can move which source a query resolves to.

    The context model goes too: its ``covers`` entries name sources, so a rename
    changes which file ``describe_source`` attaches.
    """
    for ref in tenant.sources:
        clients.invalidate_warehouse(ref.fingerprint)
    catalog.invalidate_schema(org_id)
    invalidate_context(org_id)
