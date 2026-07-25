"""Org membership and per-org ClickHouse connection management."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from datatalk import clients
from datatalk.auth import orgs as orgs_svc
from datatalk.auth import sessions as sessions_svc
from datatalk.db import clickhouse, introspect, models
from datatalk.web.deps import (
    RequestContext,
    csrf_guard,
    get_auth_session,
    get_current_user,
    get_db,
    get_request_ctx,
)

# Router-level dependencies, so a route added here cannot forget either guard.
router = APIRouter(
    prefix="/api/orgs",
    tags=["orgs"],
    dependencies=[Depends(get_current_user), Depends(csrf_guard)],
)


class CreateOrgRequest(BaseModel):
    name: str


class ConnectionRequest(BaseModel):
    host: str
    port: int = 8123
    user: str = "default"
    # Omitted or null means "keep the stored password" -- the UI never receives
    # the current one, so it cannot echo it back.
    password: str | None = None
    database: str = "default"
    secure: bool = False
    introspect_databases: list[str] | None = None
    introspect_exclude_patterns: list[str] | None = None


class TestConnectionRequest(ConnectionRequest):
    pass


def _require_admin(rctx: RequestContext) -> None:
    if rctx.tenant.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="forbidden")


def _require_same_org(rctx: RequestContext, org_id: UUID) -> None:
    if rctx.tenant.org_id != org_id:
        # 404, not 403: never confirm another org's existence.
        raise HTTPException(status_code=404, detail="org_not_found")


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


@router.get("/{org_id}/connection")
def get_connection(
    org_id: UUID,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_same_org(rctx, org_id)
    connection = orgs_svc.default_connection(db, org_id)
    if connection is None:
        return {"configured": False}
    return connection.to_public_dict()  # never includes the password


@router.put("/{org_id}/connection")
def put_connection(
    org_id: UUID,
    req: ConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    # Fingerprint BEFORE the change, so we can evict the right cache entries.
    old_fingerprint = rctx.tenant.fingerprint

    connection = orgs_svc.default_connection(db, org_id)
    if connection is None:
        connection = models.OrgClickHouseConnection(org_id=org_id, name="default", host=req.host)
        db.add(connection)

    connection.host = req.host
    connection.port = req.port
    connection.username = req.user
    connection.database = req.database
    connection.secure = req.secure
    if req.password is not None:
        connection.password = req.password  # encrypted by the column type
    if req.introspect_databases is not None:
        connection.introspect_databases = req.introspect_databases
    if req.introspect_exclude_patterns is not None:
        connection.introspect_exclude_patterns = req.introspect_exclude_patterns
    connection.updated_by_user_id = rctx.user.id
    db.flush()

    _invalidate(org_id, old_fingerprint, db)
    return connection.to_public_dict()


@router.post("/{org_id}/connection/test")
def test_connection(
    org_id: UUID,
    req: TestConnectionRequest,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Try a candidate connection without saving or caching it.

    Returns 200 with ``ok: false`` on failure: a failed *test* is a successful
    API call, and making the UI parse a 502 to show "wrong password" is worse.
    """
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    stored = orgs_svc.default_connection(db, org_id)
    password = req.password if req.password is not None else (stored.password if stored else "")

    candidate = orgs_svc.effective_settings(
        models.OrgClickHouseConnection(
            org_id=org_id,
            host=req.host,
            port=req.port,
            username=req.user,
            password=password,
            database=req.database,
            secure=req.secure,
            introspect_databases=req.introspect_databases or [],
            introspect_exclude_patterns=req.introspect_exclude_patterns or [],
        )
    )

    client = None
    try:
        # Deliberately a throwaway client, never the registry: a failed test
        # must not poison the live entry for this org.
        client = clickhouse.create_client(candidate)
        info = clickhouse.ping(client)
        tables = introspect.introspect(client, candidate, with_samples=False)
        return {
            "ok": True,
            "version": info["version"],
            "database": info["database"],
            "table_count": len(tables),
        }
    except Exception as exc:  # noqa: BLE001 - surface any driver error to the UI
        return {"ok": False, "error": str(exc)}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


@router.delete("/{org_id}/connection", status_code=204)
def delete_connection(
    org_id: UUID,
    rctx: RequestContext = Depends(get_request_ctx),
    db: Session = Depends(get_db),
) -> None:
    _require_same_org(rctx, org_id)
    _require_admin(rctx)

    connection = orgs_svc.default_connection(db, org_id)
    if connection is not None:
        old_fingerprint = rctx.tenant.fingerprint
        db.delete(connection)
        db.flush()
        _invalidate(org_id, old_fingerprint, db)


def _invalidate(org_id: UUID, old_fingerprint: str, db: Session) -> None:
    """Drop cached clients and schema for an org whose connection changed.

    Without this the org keeps querying its old warehouse, and keeps seeing the
    old schema context, until the process restarts.
    """
    clients.invalidate_clickhouse(old_fingerprint)
    introspect.invalidate_schema(org_id)
